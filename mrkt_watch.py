"""Read-only MRKT deal monitor with Telegram notifications."""

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from decimal import Decimal

try:
    from curl_cffi import requests
except ImportError:
    raise SystemExit("Не установлен curl_cffi. Запусти start_watch.command.")

from bot import ROOT, telegram
from alert_tracking import mark_offer_sold, remember_alert
from alert_format import format_market_alert
from history_store import record_offer
from market_data import record_market_activity
from runtime_settings import (
    activate_settings, active_profiles, item_allowed, source_enabled, threshold_ton,
)
from special_traits import apply_special_bases
from tonnel_watch import apply_sales_evidence, best_score, decimal, load_rate, slug

API_URL = "https://partners.swiftgifts.tg"
NANOTON = Decimal("1000000000")
BUYER_MARKUP = Decimal("1.02")
EVENT_TYPES = ("buy", "listing", "change_price")
SWIFT_REQUEST_INTERVAL = 1.05
_LAST_SWIFT_REQUEST = 0.0


class MrktAuthError(RuntimeError):
    pass


class MrktRateLimited(RuntimeError):
    def __init__(self, retry_after=60):
        self.retry_after = max(15, int(retry_after or 60))
        super().__init__(f"MRKT ограничил запросы; повтор через {self.retry_after} сек.")


def headers(token):
    return {
        "accept": "application/json",
        "content-type": "application/json",
        "x-api-key": token,
    }


def request(session, token, method, path, *, body=None, attempts=3):
    global _LAST_SWIFT_REQUEST
    last_status = None
    for attempt in range(attempts):
        try:
            wait = SWIFT_REQUEST_INTERVAL - (time.monotonic() - _LAST_SWIFT_REQUEST)
            if wait > 0:
                time.sleep(wait)
            response = session.request(
                method, API_URL + path, headers=headers(token), json=body, timeout=20
            )
            _LAST_SWIFT_REQUEST = time.monotonic()
            last_status = response.status_code
            if response.status_code == 200:
                return response.json()
            if response.status_code == 429:
                try:
                    delay = int(response.headers.get("retry-after", "60"))
                except (TypeError, ValueError):
                    delay = 60
                raise MrktRateLimited(delay)
            if response.status_code in (401, 403):
                raise MrktAuthError(
                    "Swift Gifts отклонил партнёрский ключ или не разрешил маршрут MRKT"
                )
            if response.status_code == 402:
                raise MrktAuthError("Партнёрский ключ Swift Gifts неактивен")
            if response.status_code not in (500, 502, 503, 504):
                break
        except (MrktAuthError, MrktRateLimited):
            raise
        except (OSError, ValueError):
            pass
        if attempt + 1 < attempts:
            time.sleep(attempt + 1)
    raise RuntimeError(
        f"Партнёрский источник MRKT временно недоступен "
        f"(HTTP {last_status or 'network'})"
    )


def nano_to_ton(value):
    return decimal(value, "цена MRKT") / NANOTON


def listing_query(*, collection=None, model=None, backdrop=None, count=20):
    query = {"market": ["mrkt"], "sort_by": "price asc", "fee": 0}
    if collection:
        query["collections"] = [collection]
    if model:
        query["models"] = [model]
    if backdrop:
        query["backdrops"] = [backdrop]
    return query


def feed_query(event_type="listing"):
    if event_type not in EVENT_TYPES:
        raise ValueError("Неизвестный тип события Swift Gifts")
    return {"type": event_type}


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _attribute_name(attributes, name):
    value = (attributes or {}).get(name)
    if isinstance(value, dict):
        return value.get("name")
    return value if isinstance(value, str) else None


def swift_raw_gift(row):
    """Convert one documented Swift Gifts record to the existing internal shape."""
    if not isinstance(row, dict):
        return None
    normalized = row.get("normalized") or {}
    gift = normalized.get("gift") or {}
    price_data = normalized.get("price") or {}
    attributes = gift.get("attributes") or row.get("attributes") or {}
    price_ton = next((value for value in (
        row.get("seller_price_ton"), row.get("sale_price_ton"),
        price_data.get("seller_ton"), price_data.get("ton"),
        row.get("price_ton"), row.get("price"), row.get("totalPrice"),
    ) if value is not None), None)
    try:
        price_nano = int(decimal(price_ton, "цена MRKT") * NANOTON)
    except (TypeError, ValueError):
        return None
    gift_id = gift.get("id") or row.get("giftId") or row.get("gift_id")
    if not gift_id:
        return None
    premarket = gift.get("premarket")
    if premarket is None:
        premarket = row.get("premarket")
    on_sale = gift.get("on_sale")
    if on_sale is None:
        on_sale = row.get("on_sale", True)
    return {
        "id": str(gift_id),
        "number": gift.get("number") or row.get("number"),
        "collectionName": gift.get("collection") or row.get("collection")
        or row.get("title"),
        "modelName": _attribute_name(attributes, "model") or row.get("model"),
        "backdropName": _attribute_name(attributes, "backdrop") or row.get("backdrop"),
        "symbolName": _attribute_name(attributes, "symbol") or row.get("symbol"),
        "salePrice": price_nano,
        "isOnSale": on_sale is not False,
        "isOnAuction": bool(row.get("on_auction")),
        "isMine": False,
        "isLocked": False,
        "isLockedForSale": False,
        "premarketStatus": "Premarket" if premarket else None,
        "nextResaleDate": row.get("resale_available_at"),
        "listingUrl": gift.get("webapp_url") or row.get("webapp_url"),
        "telegramUrl": gift.get("telegram_url") or row.get("link"),
        "imageUrl": gift.get("image_url") or row.get("photo_url")
        or row.get("source_image_url"),
    }


def swift_event(row, event_type):
    raw = swift_raw_gift(row)
    if raw is None:
        return None
    normalized = row.get("normalized") or {}
    market = normalized.get("market") or {}
    price_data = normalized.get("price") or {}
    native_type = str(market.get("event_type") or row.get("event_type") or event_type)
    mapped_type = {
        "buy": "Sale", "purchase": "Sale", "sale": "Sale",
        "change_price": "ChangePrice", "changeprice": "ChangePrice",
        "listing": "Listing",
    }.get(native_type.casefold(), {
        "buy": "Sale", "change_price": "ChangePrice", "listing": "Listing",
    }[event_type])
    event_id = market.get("event_id") or row.get("event_id")
    if event_id is None:
        event_id = ":".join((
            event_type, raw["id"], str(row.get("date") or row.get("listed_at") or ""),
            str(raw["salePrice"]),
        ))
    amount = price_data.get("nano") or raw["salePrice"]
    return {
        "id": str(event_id),
        "type": mapped_type,
        "amount": int(amount),
        "date": row.get("date") or row.get("listed_at"),
        "gift": raw,
    }


def mrkt_item(raw, event=None):
    if not isinstance(raw, dict):
        return None
    price_raw = raw.get("salePrice")
    if isinstance(event, dict) and event.get("amount") is not None:
        price_raw = event["amount"]
    try:
        price = nano_to_ton(price_raw)
    except ValueError:
        return None
    gift_id = raw.get("id")
    if not gift_id or price <= 0:
        return None
    if raw.get("isOnAuction") is True or raw.get("isOnSale") is False or raw.get("isMine") is True:
        return None
    if raw.get("premarketStatus") not in (None, "", "None"):
        return None
    unlock = parse_time(raw.get("nextResaleDate") or raw.get("unlockDate") or raw.get("returnLockedUntil"))
    if raw.get("isLocked") or raw.get("isLockedForSale"):
        return None
    if unlock is not None and unlock > time.time():
        return None
    return {
        "gift_id": str(gift_id),
        "gift_num": raw.get("number"),
        "name": raw.get("collectionName") or raw.get("collectionTitle") or raw.get("title"),
        "model": raw.get("modelName"),
        "backdrop": raw.get("backdropName"),
        "symbol": raw.get("symbolName"),
        "price": price,
        "status": "forsale",
        "asset": "TON",
        "auction": False,
        "dutchAuctionData": None,
        "premarketData": None,
        "underLoan": False,
        "listing_event_id": str(event.get("id")) if isinstance(event, dict) and event.get("id") else None,
        "listing_url": raw.get("listingUrl"),
        "telegram_url": raw.get("telegramUrl"),
        "image_url": raw.get("imageUrl"),
    }


def fetch_book(session, token, *, collection, model=None, backdrop=None, count=20):
    payload = request(
        session, token, "POST", f"/api/aggregator?page=0&limit={min(200, max(1, int(count)))}",
        body=listing_query(collection=collection, model=model, backdrop=backdrop, count=count),
    )
    rows = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("Swift Gifts изменил схему каталога MRKT")
    meta = payload.get("meta") or {}
    if meta.get("partial") or meta.get("providerErrors") or meta.get("providers_disabled"):
        raise RuntimeError("Партнёрский источник сообщил неполное покрытие MRKT")
    book = []
    for row in rows:
        if str(row.get("provider") or "").casefold() != "mrkt":
            continue
        raw = swift_raw_gift(row)
        item = mrkt_item(raw) if raw else None
        if item is not None:
            book.append(item)
    return book


def cached_book(db, session, token, *, collection, config, model=None, backdrop=None):
    db.execute(
        "CREATE TABLE IF NOT EXISTS mrkt_books ("
        "collection TEXT, model TEXT, backdrop TEXT, payload TEXT, checked REAL, "
        "PRIMARY KEY (collection, model, backdrop))"
    )
    db.commit()
    model_key = model or ""
    backdrop_key = backdrop or ""
    row = db.execute(
        "SELECT payload, checked FROM mrkt_books WHERE collection=? AND model=? AND backdrop=?",
        (collection, model_key, backdrop_key),
    ).fetchone()
    ttl = int(config.get("mrkt_book_cache_seconds", 20))
    if row and time.time() - float(row[1]) < ttl:
        try:
            payload = json.loads(row[0])
            if isinstance(payload, list):
                return payload
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    count = max(int(config.get("floor_limit", 12)), int(config.get("variant_floor_limit", 10)))
    book = fetch_book(
        session, token, collection=collection, model=model, backdrop=backdrop, count=count
    )
    db.execute(
        "INSERT OR REPLACE INTO mrkt_books VALUES (?, ?, ?, ?, ?)",
        (collection, model_key, backdrop_key, json.dumps(book, default=str), time.time()),
    )
    db.commit()
    return book


def mrkt_scoring_config(config):
    result = dict(config)
    result["tonnel_round_trip_cost_fraction"] = config.get(
        "mrkt_round_trip_cost_fraction", str(Decimal("1") - Decimal("1") / BUYER_MARKUP)
    )
    return result


def record_sale(event, db):
    if not isinstance(event, dict) or str(event.get("type", "")).casefold() != "sale":
        return False
    raw = event.get("gift")
    if not isinstance(raw, dict) or not event.get("id"):
        return False
    try:
        price = nano_to_ton(event.get("amount") or raw.get("salePrice"))
    except ValueError:
        return False
    happened = parse_time(event.get("date") or event.get("createdAt")) or time.time()
    db.execute(
        "CREATE TABLE IF NOT EXISTS tonnel_sales ("
        "event_id TEXT PRIMARY KEY, gift_id TEXT, gift_name TEXT, model TEXT, "
        "backdrop TEXT, symbol TEXT, price TEXT, source TEXT, occurred REAL)"
    )
    before = db.total_changes
    db.execute(
        "INSERT OR IGNORE INTO tonnel_sales VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(event["id"]), str(raw.get("id", "")), raw.get("collectionName"),
            raw.get("modelName"), raw.get("backdropName"), raw.get("symbolName"),
            str(price), "mrkt", happened,
        ),
    )
    db.commit()
    return db.total_changes > before


def listing_link(item):
    direct = str(item.get("listing_url") or "")
    if direct.startswith("https://"):
        return direct
    event_id = item.get("listing_event_id")
    if event_id:
        start_param = "feed" + str(event_id).replace("-", "")
    else:
        start_param = "gift" + str(item["gift_id"]).replace("-", "")
    return f"https://t.me/mrkt/app?startapp={start_param}"


def format_alert(item, result, config):
    return format_market_alert(item, result, market="MRKT", config=config)


def attach_cross_market_floors(
        item, result, config, secrets, db, *, session=None, token=None):
    if result is None:
        return None
    from portals_watch import add_portals_floor

    return add_portals_floor(
        item, result, config, secrets, db, base_market="MRKT",
        swift_session=session, swift_api_key=token,
    )


def send_if_new_price(item, result, config, secrets, db, send=True, *, chat_id=None):
    recipient = str(
        chat_id if chat_id is not None else secrets.get("chat_id", "__default__")
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS mrkt_alerts_by_chat "
        "(chat_id TEXT NOT NULL, id TEXT NOT NULL, price TEXT, sent REAL, "
        "PRIMARY KEY (chat_id, id))"
    )
    key = str(item["gift_id"])
    old = db.execute(
        "SELECT price FROM mrkt_alerts_by_chat WHERE chat_id=? AND id=?",
        (recipient, key),
    ).fetchone()
    owner = recipient == str(secrets.get("chat_id", "__default__"))
    if old is None and owner:
        db.execute(
            "CREATE TABLE IF NOT EXISTS mrkt_alerts "
            "(id TEXT PRIMARY KEY, price TEXT, sent REAL)"
        )
        old = db.execute(
            "SELECT price FROM mrkt_alerts WHERE id=?", (key,)
        ).fetchone()
    if old and result["price"] >= decimal(old[0]):
        return False
    text = format_alert(item, result, config)
    if not send:
        print(text + "\n")
        return False
    nft_slug = slug(item)
    preview_url = f"https://t.me/nft/{nft_slug}" if nft_slug else None
    chat_secrets = dict(secrets, chat_id=recipient)
    message_id = telegram(
        chat_secrets, text, preview_url=preview_url, button_url=listing_link(item)
    )
    remember_alert(
        db, "MRKT", item, result, message_id, preview_url=preview_url,
        chat_id=recipient,
    )
    record_offer(
        db, secrets, recipient, "MRKT", item, result,
        listing_link(item), preview_url,
    )
    db.execute(
        "INSERT OR REPLACE INTO mrkt_alerts_by_chat VALUES (?, ?, ?, ?)",
        (recipient, key, str(result["price"]), time.time()),
    )
    if owner:
        db.execute(
            "INSERT OR REPLACE INTO mrkt_alerts VALUES (?, ?, ?)",
            (key, str(result["price"]), time.time()),
        )
    db.commit()
    return True


def process_event(event, config, secrets, db, session, token, send=True):
    if not isinstance(event, dict) or not event.get("id"):
        return False
    event_id = str(event["id"])
    db.execute(
        "CREATE TABLE IF NOT EXISTS mrkt_events "
        "(id TEXT PRIMARY KEY, processed REAL)"
    )
    if db.execute("SELECT 1 FROM mrkt_events WHERE id=?", (event_id,)).fetchone():
        return False
    raw = event.get("gift")
    event_type = str(event.get("type", "")).casefold()
    if event_type == "sale":
        kind = "sale"
    elif event_type == "changeprice":
        kind = "price"
    else:
        kind = "listing"
    if isinstance(raw, dict):
        record_market_activity(
            db, f"mrkt:{event_id}", "mrkt", raw.get("collectionName"), kind,
            parse_time(event.get("date") or event.get("createdAt")) or time.time(),
        )
    record_sale(event, db)
    if event_type == "sale" and isinstance(raw, dict) and raw.get("id") is not None:
        sale_price = None
        try:
            sale_price = nano_to_ton(event.get("amount") or raw.get("salePrice"))
        except ValueError:
            pass
        mark_offer_sold(
            db, "MRKT", raw["id"], secrets, sale_price=sale_price, send=send
        )
    # Never keep the activity transaction open during floor-book network requests.
    db.commit()
    delivered = False
    if event_type in ("listing", "changeprice"):
        item = mrkt_item(event.get("gift"), event)
        if item is not None:
            rate = load_rate(db)
            profiles = [
                (chat_id, user_config)
                for chat_id, user_config in active_profiles(config)
                if source_enabled(user_config, "mrkt")
                and item_allowed(item, user_config)
                and item["price"] <= threshold_ton(
                    user_config, "max_lot", rate
                )
            ]
            if profiles:
                collection = item.get("name")
                collection_book = cached_book(
                    db, session, token, collection=collection, config=config
                )
                variant_books = {}
                if item.get("model"):
                    variant_books[("model", item["model"])] = cached_book(
                        db, session, token, collection=collection,
                        model=item["model"], config=config,
                    )
                if item.get("backdrop"):
                    variant_books[("backdrop", item["backdrop"])] = cached_book(
                        db, session, token, collection=collection,
                        backdrop=item["backdrop"], config=config,
                    )
                delivered = 0
                for chat_id, user_config in profiles:
                    score_config = mrkt_scoring_config(user_config)
                    result = best_score(
                        item, collection_book, variant_books, rate, score_config
                    )
                    result = apply_special_bases(item, result, score_config, db)
                    result = apply_sales_evidence(item, result, db, score_config)
                    result = attach_cross_market_floors(
                        item, result, user_config, secrets, db,
                        session=session, token=token,
                    )
                    if result is not None:
                        delivered += int(send_if_new_price(
                            item, result, user_config, secrets, db, send,
                            chat_id=chat_id,
                        ))
    now = time.time()
    db.execute("INSERT OR REPLACE INTO mrkt_events VALUES (?, ?)", (event_id, now))
    db.execute("DELETE FROM mrkt_events WHERE processed < ?", (now - 8 * 86400,))
    db.commit()
    return delivered


def _action_rows(payload):
    rows = payload.get("markets") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("Swift Gifts изменил схему событий MRKT")
    flattened = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        nested = row.get("data")
        if isinstance(nested, list):
            flattened.extend(item for item in nested if isinstance(item, dict))
        else:
            flattened.append(row)
    return flattened


def fetch_feed(session, token, count=20, event_types=EVENT_TYPES):
    events = []
    limit = min(100, max(1, int(count)))
    for event_type in event_types:
        payload = request(
            session, token, "POST",
            f"/api/actions/services/mrkt?page=0&limit={limit}",
            body=feed_query(event_type),
        )
        for row in _action_rows(payload):
            event = swift_event(row, event_type)
            if event is not None:
                events.append(event)
    return events


def run_cycle(config, secrets, db, session, token, send=True):
    if not any(
        source_enabled(user_config, "mrkt")
        for _, user_config in active_profiles(config)
    ):
        return 0
    events = fetch_feed(session, token, int(config.get("mrkt_feed_limit", 20)))
    delivered = 0
    for event in reversed(events):
        delivered += int(process_event(event, config, secrets, db, session, token, send))
    print(f"MRKT: проверено {len(events)} событий; отправлено {delivered} офферов.", flush=True)
    return delivered


def run(config, secrets, db, *, send=True, duration=3300, fail_fast=False):
    token = str(secrets.get("swift_api_key", "")).strip()
    if not token:
        if fail_fast:
            raise RuntimeError("SWIFT_API_KEY не настроен")
        print(
            "MRKT: партнёрский ключ Swift Gifts ещё не настроен; "
            "мониторы Tonnel и Portals продолжают работу.",
            flush=True,
        )
        return 0
    db.execute(
        "CREATE TABLE IF NOT EXISTS tonnel_stream_state "
        "(key TEXT PRIMARY KEY, value TEXT)"
    )
    db.execute(
        "INSERT OR IGNORE INTO tonnel_stream_state VALUES ('sales_started_at', ?)",
        (str(time.time()),),
    )
    db.commit()
    deadline = time.monotonic() + max(1, int(duration))
    interval = max(3, int(config.get("mrkt_poll_seconds", 5)))
    delivered = 0
    with requests.Session(impersonate="chrome") as session:
        while time.monotonic() < deadline:
            delay = interval
            try:
                delivered += run_cycle(config, secrets, db, session, token, send)
            except MrktRateLimited as exc:
                if fail_fast:
                    raise RuntimeError(str(exc)) from exc
                delay = min(exc.retry_after, 300)
                print(str(exc), file=sys.stderr, flush=True)
            except MrktAuthError as exc:
                if fail_fast:
                    raise RuntimeError(str(exc)) from exc
                print(f"MRKT остановлен: {exc}", file=sys.stderr, flush=True)
                return delivered
            except RuntimeError as exc:
                if fail_fast:
                    raise
                print(f"MRKT: цикл пропущен: {exc}", file=sys.stderr, flush=True)
            except sqlite3.OperationalError as exc:
                db.rollback()
                if fail_fast:
                    raise
                print(f"MRKT: база занята, цикл будет повторён: {exc}", file=sys.stderr, flush=True)
            time.sleep(min(delay, max(0, deadline - time.monotonic())))
    return delivered


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--duration", type=int, default=3300)
    parser.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args()
    config = json.loads((ROOT / "config.json").read_text())
    secrets = json.loads((ROOT / "secrets.json").read_text())
    duration = 1 if args.once else args.duration
    with sqlite3.connect(ROOT / "settings.sqlite3", timeout=5) as settings_db:
        settings_db.execute("PRAGMA busy_timeout=5000")
        settings_db.execute("PRAGMA journal_mode=WAL")
        activate_settings(settings_db, secrets["chat_id"], secrets)
        with sqlite3.connect(ROOT / "state.sqlite3", timeout=30) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=30000")
            run(
                config, secrets, db, send=args.send, duration=duration,
                fail_fast=args.fail_fast,
            )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        print(f"Не удалось запустить MRKT: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("Мониторинг MRKT остановлен.")
